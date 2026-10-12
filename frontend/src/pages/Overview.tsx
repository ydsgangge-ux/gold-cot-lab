import { useEffect, useMemo, useState } from 'react'
import {
  api,
  INVALID_REASON_LABELS,
  MarketOption,
  Overview as OverviewPayload,
  PricePoint,
  SeriesRow,
} from '../api'
import { Selection } from '../components/FilterBar'
import { GlossaryPanel, Term } from '../components/Term'
import PositionChart from '../charts/PositionChart'
import { clampPct, num, pct, signed, stampUtc } from '../format'

interface Props {
  selection: Selection
  startDate: string
  activeMarket: MarketOption | undefined
}

/** Backend alert codes spelled out, because "normal" helps nobody. */
const ALERT_LABELS: Record<string, string> = {
  normal: '目前不属于极端位置',
  '高位拥挤观察': '净持仓处于历史相对高位（高于 90% 分位）· 拥挤观察',
  '低位拥挤观察': '净持仓处于历史相对低位（低于 10% 分位）· 拥挤观察',
  unknown: '样本不足，暂时判断不了',
}

function buildQuery(selection: Selection, startDate: string, start: boolean): string {
  const params = new URLSearchParams({
    report_type: selection.reportType,
    market_code: selection.marketCode,
    pit_mode: selection.pitMode,
  })
  if (selection.asOf) params.set('as_of', selection.asOf)
  if (start && startDate) params.set('start', startDate)
  return params.toString()
}

/**
 * Put a gold close next to every weekly report.
 *
 * The price attached to a report is the last close on or before the day the
 * report became public — not the report date. Attaching the report date's close
 * would quietly use a price from after the data was known, which is exactly the
 * look-ahead this tool exists to avoid.
 */
function alignPriceToReports(rows: SeriesRow[], prices: PricePoint[]): Array<number | null> {
  if (!prices.length) return rows.map(() => null)
  const sorted = [...prices].sort((a, b) => a.trade_date.localeCompare(b.trade_date))
  const aligned: Array<number | null> = []
  let cursor = 0
  let latest: number | null = null
  for (const row of rows) {
    const visibleFrom = (row.published_at || row.report_date).slice(0, 10)
    while (cursor < sorted.length && sorted[cursor].trade_date <= visibleFrom) {
      const close = sorted[cursor].close
      if (typeof close === 'number') latest = close
      cursor += 1
    }
    aligned.push(latest)
  }
  return aligned
}

function arrow(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return ''
  if (value > 0) return '▲ '
  if (value < 0) return '▼ '
  return '－ '
}

function directionWord(value: number | null | undefined, up: string, down: string): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  if (value > 0) return up
  if (value < 0) return down
  return '没有变化'
}

function IndicatorLine({
  overview,
  code,
  label,
  termKey,
}: {
  overview: OverviewPayload
  code: string
  label: string
  termKey?: string
}) {
  const indicator = overview.indicators[code]
  if (!indicator) return null
  const text =
    typeof indicator.value === 'number'
      ? code.startsWith('cot_index') || code.startsWith('pct_rank') || code === 'net_oi_pct'
        ? pct(indicator.value, 1)
        : num(indicator.value, 0)
      : String(indicator.value ?? '—')
  return (
    <>
      <dt>{termKey ? <Term k={termKey}>{label}</Term> : label}</dt>
      <dd>
        {indicator.is_valid ? text : '暂时算不出'}
        {!indicator.is_valid && indicator.invalid_reason && (
          <span className="meta" style={{ color: '#9aa1a9', marginLeft: 6 }}>
            {INVALID_REASON_LABELS[indicator.invalid_reason] ?? indicator.invalid_reason}
          </span>
        )}
      </dd>
    </>
  )
}

export default function Overview({ selection, startDate, activeMarket }: Props) {
  const [overview, setOverview] = useState<OverviewPayload | null>(null)
  const [rows, setRows] = useState<SeriesRow[]>([])
  const [prices, setPrices] = useState<PricePoint[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    Promise.all([
      api.overview(buildQuery(selection, startDate, false)),
      api.series(buildQuery(selection, startDate, true)),
      // A price failure must not blank the page: the COT side stands alone.
      api.priceSeries().catch(() => ({ series: [] as PricePoint[] })),
    ])
      .then(([nextOverview, nextSeries, nextPrices]) => {
        if (cancelled) return
        setOverview(nextOverview)
        setRows(nextSeries.series)
        setPrices(nextPrices.series)
      })
      .catch((reason: Error) => {
        if (!cancelled) setError(reason.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [selection.reportType, selection.marketCode, selection.asOf, selection.pitMode, startDate])

  const recent = useMemo(() => rows.slice(-8).reverse(), [rows])
  const priceSeries = useMemo(() => alignPriceToReports(rows, prices), [rows, prices])
  const hasPrice = useMemo(
    () => priceSeries.some((value) => typeof value === 'number'),
    [priceSeries],
  )
  const lastPrice = useMemo(() => {
    for (let i = priceSeries.length - 1; i >= 0; i -= 1) {
      if (typeof priceSeries[i] === 'number') return priceSeries[i]
    }
    return null
  }, [priceSeries])

  const rank = overview?.indicators.pct_rank_3y
  const alert = overview?.indicators.alert
  const divergence = overview?.indicators.divergence

  if (error) return <div className="notice danger">读取失败：{error}</div>
  if (!overview) return <div className="notice">{loading ? '正在读取…' : '暂无数据。'}</div>

  const position = overview.position
  const changeClass = (position.net_change ?? 0) > 0 ? 'up' : (position.net_change ?? 0) < 0 ? 'down' : ''
  const netClass = (position.net ?? 0) >= 0 ? '' : 'down'
  const alertText = alert?.is_valid
    ? ALERT_LABELS[String(alert.value)] ?? String(alert.value)
    : '样本不足，暂时判断不了'

  return (
    <>
      <section className="panel">
        <h2>当前持仓快照</h2>
        <div className="cards">
          <div className="card">
            <div className="label">
              <Term k="report_date">数据截至（持仓统计日）</Term>
            </div>
            <div className="value">{overview.data_as_of}</div>
            <div className="plain">
              这一周里的<b>周二</b>，也就是持仓真正发生的那一天。
            </div>
          </div>
          <div className="card">
            <div className="label">
              <Term k="published_at">实际发布日</Term>
            </div>
            <div className="value" style={{ fontSize: 16 }}>
              {stampUtc(overview.published_at)}
            </div>
            <div className="plain">
              你<b>最早能看到</b>这份数据的时间（通常是同一周周五）。
              {overview.published_at_source === 'derived'
                ? '这里的时间是按规则推导的，是真实可见时间的下限。'
                : ''}
            </div>
          </div>
          <div className="card">
            <div className="label">
              <Term k="net">管理基金净持仓</Term>
            </div>
            <div className="value">
              {arrow(position.net)}
              <span className={netClass}>{num(position.net, 0)}</span> <small>手</small>
            </div>
            <div className="meta">
              <Term k="mm_long">多头</Term> {num(position.mm_long, 0)} ·{' '}
              <Term k="mm_short">空头</Term> {num(position.mm_short, 0)}
            </div>
            <div className="plain">
              多头减空头。<b>正数＝基金整体看涨</b>，负数＝整体看跌；数值越高说明越看好黄金。
            </div>
          </div>
          <div className="card">
            <div className="label">
              <Term k="net_change">比上一周</Term>
            </div>
            <div className={`value ${changeClass}`}>
              {arrow(position.net_change)}
              {signed(position.net_change, 0)}
            </div>
            <div className="meta">
              {position.net_streak === null ? '—' : `已经连续 ${num(position.net_streak, 0)} 周同向`}
            </div>
            <div className="plain">
              {directionWord(position.net_change, '这周在加仓看涨。', '这周在撤退减仓。')}
              净持仓变多也可能是空头认输离场，所以下面把多头和空头的变化分开列。
            </div>
          </div>
          <div className="card">
            <div className="label">
              <Term k="net_oi_pct">净持仓占整个市场</Term>
            </div>
            <div className="value">{pct(position.net_oi_pct, 2)}</div>
            <div className="meta">
              市场总规模（<Term k="open_interest">未平仓量</Term>）{num(position.open_interest, 0)} 手
            </div>
            <div className="plain">基金的方向性押注，占整个市场多大比重。</div>
          </div>
        </div>

        <div style={{ marginTop: 16 }}>
          <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 6 }}>
            <Term k="pct_rank_3y">净持仓在过去 3 年里的位置</Term>
            {rank?.sample_size ? `（用 ${rank.sample_size} 期报告比较）` : ''}
            {rank && !rank.is_valid && (
              <span style={{ color: '#b9822f', marginLeft: 8 }}>
                暂时算不出：{INVALID_REASON_LABELS[rank.invalid_reason ?? ''] ?? rank.invalid_reason}
              </span>
            )}
          </div>
          <div
            style={{
              position: 'relative',
              height: 26,
              background: 'linear-gradient(90deg,#eef7f1 0%,#f6f7f8 50%,#fdf5e6 100%)',
              border: '1px solid var(--border)',
              borderRadius: 7,
            }}
          >
            {rank?.is_valid && (
              <div
                style={{
                  position: 'absolute',
                  left: `calc(${clampPct(rank.value as number)}% - 1px)`,
                  top: -3,
                  bottom: -3,
                  width: 2,
                  background: '#3f6ea8',
                }}
              />
            )}
            <div style={{ position: 'absolute', left: 8, top: 5, fontSize: 11, color: '#9aa1a9' }}>最低 0%</div>
            <div style={{ position: 'absolute', right: 8, top: 5, fontSize: 11, color: '#9aa1a9' }}>最高 100%</div>
          </div>
          <div className="hint" style={{ marginTop: 5 }}>
            蓝线所在的位置，就是当前净持仓排在过去 3 年所有周里的名次。
            <b>越靠右＝仓位越重</b>。10% 以下是历史相对低位，90% 以上是历史相对高位——
            这只是位置标签，<b>不是买卖信号</b>。
          </div>
        </div>
      </section>

      <section className="panel">
        <h2>黄金期货走势 与 基金持仓</h2>
        <PositionChart rows={rows} priceSeries={priceSeries} hasPrice={hasPrice} />
        {hasPrice && lastPrice !== null && (
          <div className="notice" style={{ marginTop: 8 }}>
            图中最新一个交易日的 COMEX 黄金期货收盘价：
            <b> {num(lastPrice, 1)} 美元/盎司</b>。
            价格来自新浪财经的 COMEX 黄金连续合约（未做换月调整），仅供研究参考。
          </div>
        )}
        {!hasPrice && (
          <div className="notice warn" style={{ marginTop: 8 }}>
            还没有黄金价格数据，所以图里没有价格线，价格相关指标也都显示为「暂时算不出」。
            请到「数据管理」页点一下<b>「获取黄金价格」</b>。
          </div>
        )}
        <GlossaryPanel />
      </section>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(320px,1fr))', gap: 14 }}>
        <section className="panel">
          <h2>各项读数（鼠标停在词上会有解释）</h2>
          <dl className="kv">
            <IndicatorLine overview={overview} code="d_long" label="多头周变化" termKey="d_long" />
            <IndicatorLine overview={overview} code="d_short" label="空头周变化" termKey="d_short" />
            <IndicatorLine overview={overview} code="pct_rank_3y" label="3 年历史分位" termKey="pct_rank_3y" />
            <IndicatorLine overview={overview} code="pct_rank_5y" label="5 年历史分位" termKey="pct_rank_5y" />
            <IndicatorLine overview={overview} code="cot_index_26w" label="COT Index（26 周）" termKey="cot_index" />
            <IndicatorLine overview={overview} code="cot_index_52w" label="COT Index（52 周）" termKey="cot_index" />
            <IndicatorLine overview={overview} code="cot_index_156w" label="COT Index（156 周）" termKey="cot_index" />
            <IndicatorLine overview={overview} code="ret_4w" label="价格 4 周涨跌" termKey="ret" />
            <IndicatorLine overview={overview} code="trend_4w" label="4 周价格趋势" termKey="trend" />
            <IndicatorLine overview={overview} code="divergence" label="价格与仓位是否一致" termKey="divergence" />
          </dl>
        </section>

        <section className="panel">
          <h2>这一期该怎么读</h2>
          <dl className="kv">
            <dt>
              <Term k="alert">极端仓位提示</Term>
            </dt>
            <dd>{alertText}</dd>
            <dt>
              <Term k="divergence">价格与仓位是否一致</Term>
            </dt>
            <dd>
              {divergence?.is_valid ? (
                String(divergence.value)
              ) : (
                <span>
                  暂时算不出 ·
                  <span style={{ color: '#9aa1a9', marginLeft: 4 }}>
                    {INVALID_REASON_LABELS[divergence?.invalid_reason ?? ''] ?? divergence?.invalid_reason}
                  </span>
                </span>
              )}
            </dd>
          </dl>
          <div className="notice" style={{ marginTop: 10 }}>
            {divergence?.is_valid
              ? '价格方向和基金持仓方向已经可以比较。如果两者不一致（例如价格在涨但基金在减仓），说明这波行情可能不是基金推动的——值得再多看一眼，但不代表行情必然反转。'
              : '缺少黄金价格数据，所以判断不了价格和持仓是否一致。价格相关的指标现在一律保持为「暂时算不出」，不会填 0 来充数。'}
          </div>
          <div className="hint" style={{ marginTop: 10 }}>
            <b>本期系统给出的说明：</b>
          </div>
          <ul style={{ fontSize: 12.5, color: '#6b7280', margin: '6px 0 0', paddingLeft: 18 }}>
            {overview.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </section>
      </div>

      <section className="panel">
        <h2>最近 8 周逐周对照</h2>
        <table className="data">
          <thead>
            <tr>
              <th title="持仓真正发生的那一周周二">持仓统计日</th>
              <th title="你最早能看到这份报告的时间">公开时间</th>
              <th title="基金押注上涨的合约数">多头</th>
              <th title="基金押注下跌的合约数">空头</th>
              <th title="多头减空头，正数＝整体看涨">净持仓</th>
              <th title="比上一周多了多少，正数＝加仓看涨">净变化</th>
              <th title="净持仓占整个市场的比例">净持仓/市场</th>
              <th title="净持仓在过去 3 年里排第几，越靠右越拥挤">3 年分位</th>
              <th title="净持仓压缩到 0～100 的刻度，100＝窗口内最高">COT Index 52 周</th>
            </tr>
          </thead>
          <tbody>
            {recent.map((row) => {
              const rankState = row['pct_rank_3y__state'] as { is_valid: number } | undefined
              const change = typeof row.d_net === 'number' ? row.d_net : null
              return (
                <tr key={row.report_date}>
                  <td>{row.report_date}</td>
                  <td>{stampUtc(row.published_at)}</td>
                  <td>{num(row.mm_long, 0)}</td>
                  <td>{num(row.mm_short, 0)}</td>
                  <td>{num(row.net, 0)}</td>
                  <td style={{ color: (change ?? 0) > 0 ? '#3f8a63' : (change ?? 0) < 0 ? '#b4483f' : undefined }}>
                    {arrow(change)}
                    {signed(change, 0)}
                  </td>
                  <td>{typeof row.net_oi_pct === 'number' ? pct(row.net_oi_pct, 2) : '—'}</td>
                  <td>
                    {typeof row.pct_rank_3y === 'number' && rankState?.is_valid !== 0
                      ? pct(row.pct_rank_3y, 1)
                      : '—'}
                  </td>
                  <td>{typeof row.cot_index_52w === 'number' ? num(row.cot_index_52w, 1) : '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <div className="chart-note">
          「净变化」上标的 ▲ 表示比上一周加仓看涨，▼ 表示减仓撤退。表格里的横线「—」表示算不出，而不是 0。
        </div>
      </section>

      <div className="notice">
        范围：<span className="mono">{startDate || activeMarket?.first_date || '全部'}</span> 起，
        共 <span className="mono">{rows.length}</span> 期；
        可见性口径 <span className="mono">{overview.pit_mode}</span>，
        读取时刻 <span className="mono">{overview.as_of}</span>，
        计算版本 <span className="mono">{overview.latest_version}</span>。
      </div>
    </>
  )
}