import { useEffect, useMemo, useState } from 'react'
import {
  api,
  IndicatorState,
  INVALID_REASON_LABELS,
  PricePoint,
  PriceSeries,
  SeriesRow,
} from '../api'
import { Selection } from '../components/FilterBar'
import { GlossaryPanel, Term } from '../components/Term'
import WeeklyPriceChart from '../charts/WeeklyPriceChart'
import { num, pct, signed, stampUtc } from '../format'

interface Props {
  selection: Selection
  startDate: string
  onAsOf: (value: string) => void
}

const HORIZONS = [1, 2, 4, 8, 12]
const CHART_HORIZON = 12

const ALERT_LABELS: Record<string, string> = {
  normal: '目前不属于极端位置',
  '高位拥挤观察': '净持仓处于历史相对高位（高于 90% 分位）· 拥挤观察',
  '低位拥挤观察': '净持仓处于历史相对低位（低于 10% 分位）· 拥挤观察',
  unknown: '样本不足，暂时判断不了',
}

function addDays(isoDate: string, days: number): string {
  const parts = isoDate.slice(0, 10).split('-').map(Number)
  if (parts.length !== 3 || parts.some(Number.isNaN)) return isoDate
  const shifted = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2] + days))
  return shifted.toISOString().slice(0, 10)
}

interface Review {
  anchorTradeDate: string
  anchorClose: number
  segment: PricePoint[]
  forwards: Array<{ weeks: number; targetDate: string; close: number | null; change: number | null }>
}

/**
 * Rebuild "what you could see that day, and what price did next".
 *
 * The anchor close is the last close on or before the report's release date —
 * the same alignment the overview chart uses, so the two pages cannot disagree
 * about which price belonged to which report. Everything after the anchor is
 * explicitly the future; the chart shades it so no one mistakes it for
 * something that was known at the time.
 */
function computeReview(prices: PricePoint[], releaseDate: string): Review | null {
  const sorted = prices
    .filter((point) => typeof point.close === 'number')
    .sort((a, b) => a.trade_date.localeCompare(b.trade_date))
  if (!sorted.length) return null

  let anchorIndex = -1
  for (let i = 0; i < sorted.length; i += 1) {
    if (sorted[i].trade_date <= releaseDate) anchorIndex = i
    else break
  }
  if (anchorIndex < 0) return null
  const anchor = sorted[anchorIndex]
  const anchorClose = anchor.close as number

  const forwards = HORIZONS.map((weeks) => {
    const target = addDays(anchor.trade_date, weeks * 7)
    let hit = -1
    for (let i = anchorIndex; i < sorted.length; i += 1) {
      if (sorted[i].trade_date <= target) hit = i
      else break
    }
    const close = hit >= 0 ? (sorted[hit].close as number) : null
    return {
      weeks,
      targetDate: hit >= 0 ? sorted[hit].trade_date : '',
      close,
      change: close === null ? null : (close / anchorClose - 1) * 100,
    }
  })

  const from = addDays(anchor.trade_date, -28)
  const to = addDays(anchor.trade_date, CHART_HORIZON * 7)
  const segment = sorted.filter((point) => point.trade_date >= from && point.trade_date <= to)

  return { anchorTradeDate: anchor.trade_date, anchorClose, segment, forwards }
}

function stateOf(row: SeriesRow, key: string): IndicatorState | undefined {
  return row[`${key}__state`] as IndicatorState | undefined
}

function isValid(row: SeriesRow, key: string): boolean {
  const state = stateOf(row, key)
  return !state || state.is_valid !== 0
}

function snapshotQuery(selection: Selection, startDate: string, withAsOf: boolean): string {
  const params = new URLSearchParams({
    report_type: selection.reportType,
    market_code: selection.marketCode,
    pit_mode: selection.pitMode,
  })
  if (withAsOf && selection.asOf) params.set('as_of', selection.asOf)
  if (startDate) params.set('start', startDate)
  return params.toString()
}

export default function Weekly({ selection, startDate, onAsOf }: Props) {
  const [rows, setRows] = useState<SeriesRow[]>([])
  const [recentDates, setRecentDates] = useState<string[]>([])
  const [price, setPrice] = useState<PriceSeries | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    Promise.all([
      api.series(snapshotQuery(selection, startDate, true)),
      // The same series without a cut-off, purely to offer quick-pick dates.
      api.series(snapshotQuery(selection, '', false)),
      api.priceSeries().catch(() => null),
    ])
      .then(([visible, full, nextPrice]) => {
        if (cancelled) return
        setRows(visible.series)
        setRecentDates(full.series.slice(-12).map((row) => row.report_date).reverse())
        setPrice(nextPrice)
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

  const visible = rows.length ? rows[rows.length - 1] : null
  const review = useMemo(() => {
    if (!visible || !price?.series.length) return null
    return computeReview(price.series, (visible.published_at || visible.report_date).slice(0, 10))
  }, [visible, price])

  if (error) return <div className="notice danger">读取失败：{error}</div>

  const hasPrice = Boolean(price?.available && price.series.length)

  return (
    <>
      <section className="panel">
        <h2>挑一个回看日期</h2>
        <div className="hint" style={{ marginBottom: 8 }}>
          把界面还原到<b>过去某一天</b>：只使用那一天已经公开的报告，看看当时能得出什么结论，
          然后对照<b>之后</b>价格实际怎么走。这是唯一一处会用到「未来」的地方，而且只用于事后核对，
          绝不参与当时的结论。
        </div>
        <div className="row">
          <span style={{ fontSize: 12, color: '#6b7280' }}>回看日期：</span>
          <input
            type="date"
            value={selection.asOf.slice(0, 10)}
            onChange={(event) => onAsOf(event.target.value)}
            style={{ font: 'inherit', padding: '5px 8px', border: '1px solid var(--border-strong)', borderRadius: 7 }}
          />
          <button className="action" onClick={() => onAsOf('')} disabled={!selection.asOf}>
            回到最新
          </button>
          <span className="spacer" />
          <span style={{ fontSize: 12, color: '#9aa1a9' }}>
            可见性口径 {selection.pitMode}
          </span>
        </div>

        {recentDates.length > 0 && (
          <div className="row" style={{ marginTop: 8 }}>
            <span style={{ fontSize: 12, color: '#6b7280' }}>最近的报告：</span>
            {recentDates.map((date) => (
              <button
                key={date}
                className="action"
                onClick={() => onAsOf(date)}
                style={
                  selection.asOf.slice(0, 10) === date
                    ? { background: 'var(--accent-soft)', borderColor: '#b9cbe2', fontWeight: 600 }
                    : undefined
                }
              >
                {date}
              </button>
            ))}
          </div>
        )}

        <div className="notice warn">
          <b>关于「公开时间」的诚实说明：</b>
          本页按「公开时间」口径还原当天可见的数据，但这里的公开时间是<b>按规则推导的下限</b>
          （报告日那周的周五 15:30 美东），<b>不是 CFTC 的官方发布时间戳</b>。
          因此它无法反映政府停摆导致的延期补发，也无法反映事后对历史数据的修正。
        </div>
        {selection.pitMode === 'report_date' && (
          <div className="notice danger">
            你选的是「按报告日」，这会看到<b>当时还拿不到的数据</b>，属于前视偏差，不能用来看当时能得出什么结论。
            请把可见性口径改回默认的「按公开时间」。
          </div>
        )}
      </section>

      <section className="panel">
        <h2>那一天能看到什么</h2>
        {!visible ? (
          <div className="notice">{loading ? '正在读取…' : '这个日期之前没有任何可见的报告，请把日期往后调一点。'}</div>
        ) : (
          <>
            <div className="cards">
              <div className="card">
                <div className="label">
                  <Term k="report_date">数据截至（持仓统计日）</Term>
                </div>
                <div className="value">{visible.report_date}</div>
                <div className="plain">当时能看到的最新一期，持仓发生在这周的周二。</div>
              </div>
              <div className="card">
                <div className="label">
                  <Term k="published_at">实际发布日</Term>
                </div>
                <div className="value" style={{ fontSize: 15 }}>
                  {stampUtc(visible.published_at)}
                </div>
                <div className="plain">这份报告最早能被看到的时间（规则推导，为真实时间的下限）。</div>
              </div>
              <div className="card">
                <div className="label">
                  <Term k="net">基金净持仓</Term>
                </div>
                <div className="value">
                  <span className={(visible.net ?? 0) < 0 ? 'down' : ''}>{num(visible.net, 0)}</span>{' '}
                  <small>手</small>
                </div>
                <div className="meta">
                  多头 {num(visible.mm_long, 0)} · 空头 {num(visible.mm_short, 0)}
                </div>
                <div className="plain">多头减空头，正数＝当时基金整体看涨。</div>
              </div>
              <div className="card">
                <div className="label">
                  <Term k="net_change">比上一周</Term>
                </div>
                <div
                  className="value"
                  style={{ color: (visible.d_net ?? 0) > 0 ? 'var(--ok)' : (visible.d_net ?? 0) < 0 ? 'var(--danger)' : undefined }}
                >
                  {signed(visible.d_net as number | null, 0)}
                </div>
                <div className="meta">
                  连续同向 {visible.net_streak === null ? '—' : num(visible.net_streak, 0)} 周
                </div>
                <div className="plain">净持仓比上一周的变化，正数＝这周在加仓看涨。</div>
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(300px,1fr))', gap: 14, marginTop: 14 }}>
              <dl className="kv">
                <dt>
                  <Term k="d_long">多头周变化</Term>
                </dt>
                <dd>{isValid(visible, 'd_long') ? signed(visible.d_long as number | null, 0) : '暂时算不出'}</dd>
                <dt>
                  <Term k="d_short">空头周变化</Term>
                </dt>
                <dd>{isValid(visible, 'd_short') ? signed(visible.d_short as number | null, 0) : '暂时算不出'}</dd>
                <dt>
                  <Term k="net_oi_pct">净持仓占整个市场</Term>
                </dt>
                <dd>
                  {typeof visible.net_oi_pct === 'number' ? pct(visible.net_oi_pct, 2) : '暂时算不出'}
                </dd>
                <dt>
                  <Term k="pct_rank_3y">3 年历史分位</Term>
                </dt>
                <dd>
                  {isValid(visible, 'pct_rank_3y') && typeof visible.pct_rank_3y === 'number'
                    ? pct(visible.pct_rank_3y, 1)
                    : '暂时算不出'}
                </dd>
                <dt>
                  <Term k="cot_index">COT Index（52 周）</Term>
                </dt>
                <dd>
                  {isValid(visible, 'cot_index_52w') && typeof visible.cot_index_52w === 'number'
                    ? num(visible.cot_index_52w, 1)
                    : '暂时算不出'}
                </dd>
              </dl>
              <dl className="kv">
                <dt>
                  <Term k="alert">极端仓位提示</Term>
                </dt>
                <dd>
                  {isValid(visible, 'alert')
                    ? ALERT_LABELS[String(visible.alert)] ?? String(visible.alert ?? '—')
                    : '样本不足，暂时判断不了'}
                </dd>
                <dt>
                  <Term k="divergence">价格与仓位是否一致</Term>
                </dt>
                <dd>
                  {isValid(visible, 'divergence')
                    ? String(visible.divergence)
                    : `暂时算不出${
                        stateOf(visible, 'divergence')?.invalid_reason
                          ? ` · ${INVALID_REASON_LABELS[stateOf(visible, 'divergence')!.invalid_reason ?? ''] ?? ''}`
                          : ''
                      }`}
                </dd>
              </dl>
            </div>
            <div className="hint" style={{ marginTop: 10 }}>
              以上每一项都只用「{visible.report_date}」这一期及更早的报告算出，含当期的分位与 COT Index。
              显示「暂时算不出」的地方是真的算不出，不会被填成 0。
            </div>
          </>
        )}
      </section>

      <section className="panel">
        <h2>随后几周，价格实际怎么走</h2>
        {!visible ? null : !hasPrice ? (
          <div className="notice warn">
            还没有黄金价格数据，所以这里看不到后续走势。请到「数据管理」页点一下<b>「获取黄金价格」</b>。
          </div>
        ) : !review ? (
          <div className="notice">回看日之前还没有价格数据，无法对照后续走势。</div>
        ) : (
          <>
            <WeeklyPriceChart
              segment={review.segment}
              anchorTradeDate={review.anchorTradeDate}
              horizonWeeks={CHART_HORIZON}
            />
            <table className="data" style={{ marginTop: 12 }}>
              <thead>
                <tr>
                  <th>回看之后</th>
                  <th>对应交易日</th>
                  <th>价格上涨 / 下跌</th>
                  <th>届时价格（美元/盎司）</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>回看日当时（基准）</td>
                  <td>{review.anchorTradeDate}</td>
                  <td>—</td>
                  <td>{num(review.anchorClose, 1)}</td>
                </tr>
                {review.forwards.map((item) => (
                  <tr key={item.weeks}>
                    <td>{item.weeks} 周后</td>
                    <td>{item.targetDate || '—'}</td>
                    <td
                      style={{
                        color: (item.change ?? 0) > 0 ? '#3f8a63' : (item.change ?? 0) < 0 ? '#b4483f' : undefined,
                      }}
                    >
                      {item.change === null ? '—' : `${item.change > 0 ? '+' : ''}${num(item.change, 2)}%`}
                    </td>
                    <td>{num(item.close, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="chart-note">
              基准价＝回看日当天（或之前最近一个交易日）的收盘价。后面的涨跌幅都是相对这个基准算的，纯属事后核对。
              <br />
              <span style={{ color: '#9aa1a9' }}>
                注意：这里的价格来自新浪财经的 COMEX 黄金<b>连续合约</b>，没有做换月调整，
                遇到合约切换的那几周，涨跌幅会被换月的价差轻微污染，所以不要把它当成精确的可交易收益。
              </span>
            </div>
          </>
        )}
      </section>

      <section className="panel">
        <GlossaryPanel />
      </section>

      <div className="notice">
        读取截至 <span className="mono">{selection.asOf || '当前时刻'}</span>，可见性口径{' '}
        <span className="mono">{selection.pitMode}</span>，共{' '}
        <span className="mono">{rows.length}</span> 期可见报告。
      </div>
    </>
  )
}