import { useEffect, useMemo, useState } from 'react'
import { api, StructureRow } from '../api'
import { Selection } from '../components/FilterBar'
import { GlossaryPanel, Term } from '../components/Term'
import StructureChart, {
  CATEGORIES,
  METRIC_LABELS,
  StructureMetric,
} from '../charts/StructureChart'
import { num, signed, stampUtc } from '../format'

interface Props {
  selection: Selection
  startDate: string
}

const METRICS: StructureMetric[] = ['net', 'long', 'short']

function buildQuery(selection: Selection, startDate: string): string {
  const params = new URLSearchParams({
    report_type: selection.reportType,
    market_code: selection.marketCode,
    pit_mode: selection.pitMode,
  })
  if (selection.asOf) params.set('as_of', selection.asOf)
  if (startDate) params.set('start', startDate)
  return params.toString()
}

/** Indexed lookups come back as a union; only real numbers are usable here. */
function asNumber(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}

function arrow(value: number | null): string {
  if (value === null) return ''
  if (value > 0) return '▲ '
  if (value < 0) return '▼ '
  return '－ '
}

function deltaColor(value: number | null): string | undefined {
  if (value === null) return undefined
  if (value > 0) return '#3f8a63'
  if (value < 0) return '#b4483f'
  return undefined
}

export default function Structure({ selection, startDate }: Props) {
  const [rows, setRows] = useState<StructureRow[]>([])
  const [metric, setMetric] = useState<StructureMetric>('net')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .structure(buildQuery(selection, startDate))
      .then((data) => {
        if (!cancelled) setRows(data.structure)
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

  const latest = rows.length ? rows[rows.length - 1] : null
  const previous = rows.length > 1 ? rows[rows.length - 2] : null

  const cards = useMemo(() => {
    if (!latest) return []
    return CATEGORIES.map((category) => {
      const net = asNumber(latest[`${category.key}_net`])
      const prevNet = previous ? asNumber(previous[`${category.key}_net`]) : null
      const change = net !== null && prevNet !== null ? net - prevNet : null
      return { category, net, change }
    })
  }, [latest, previous])

  if (error) return <div className="notice danger">读取失败：{error}</div>
  if (!latest) return <div className="notice">{loading ? '正在读取…' : '暂无数据。'}</div>

  return (
    <>
      <section className="panel">
        <h2>五类交易者，同一条时间轴</h2>
        <div className="hint" style={{ marginBottom: 10 }}>
          CFTC 的报告把市场里的人分成五类。<b>净持仓＝多头减空头</b>：正数偏看涨，负数偏看跌。
          把五类放在一起，是为了看清同一周里谁在加仓、谁在减仓——尤其是<b>基金</b>，
          它的净持仓变化到底来自「新开多头」还是「空头认输」，要靠下面拆开的多头 / 空头才能分辨。
        </div>

        <div className="row" style={{ marginBottom: 6 }}>
          <span style={{ fontSize: 12, color: '#6b7280' }}>看哪个口径：</span>
          {METRICS.map((item) => (
            <button
              key={item}
              className="action"
              onClick={() => setMetric(item)}
              style={
                metric === item
                  ? { background: 'var(--accent-soft)', borderColor: '#b9cbe2', fontWeight: 600 }
                  : undefined
              }
            >
              {METRIC_LABELS[item]}
            </button>
          ))}
          <span className="spacer" />
          <span style={{ fontSize: 12, color: '#9aa1a9' }}>
            {rows.length} 期 · 最新 {latest.report_date} · {stampUtc(latest.published_at)}
          </span>
        </div>

        <StructureChart rows={rows} metric={metric} />
      </section>

      <section className="panel">
        <h2>最新一期各类交易者净持仓</h2>
        <div className="cards">
          {cards.map(({ category, net, change }) => (
            <div className="card" key={category.key}>
              <div className="label">
                <Term k={category.term}>{category.label}</Term>
              </div>
              <div className="value" style={{ fontSize: 20 }}>
                {arrow(net)}
                <span className={net !== null && net < 0 ? 'down' : ''}>{num(net, 0)}</span>{' '}
                <small>手</small>
              </div>
              <div className="meta">
                比上一周{' '}
                <span style={{ color: deltaColor(change) }}>
                  {arrow(change)}
                  {signed(change, 0)}
                </span>
              </div>
            </div>
          ))}
        </div>
        <div className="hint" style={{ marginTop: 8 }}>
          正数是净看涨，负数是净看跌。生产商、掉期交易商常年为负，属于正常现象（它们是套保和做市的一方），
          要对比的是它们自己前后的变化，而不是和基金比大小。
        </div>
      </section>

      <section className="panel">
        <h2>最新一期拆解：多头 / 空头 分开看</h2>
        <table className="data">
          <thead>
            <tr>
              <th>交易者</th>
              <th title="押注上涨的合约数">多头</th>
              <th title="押注下跌的合约数">空头</th>
              <th title="多头减空头">净持仓</th>
              <th title="这一周多头增加了多少，正数＝新开多头">多头周变化</th>
              <th title="这一周空头增加了多少，负值＝空头在离场">空头周变化</th>
              <th title="净持仓这一周的变化">净变化</th>
            </tr>
          </thead>
          <tbody>
            {CATEGORIES.map((category) => {
              const long = asNumber(latest[`${category.key}_long`])
              const short = asNumber(latest[`${category.key}_short`])
              const net = asNumber(latest[`${category.key}_net`])
              const prevLong = previous ? asNumber(previous[`${category.key}_long`]) : null
              const prevShort = previous ? asNumber(previous[`${category.key}_short`]) : null
              const prevNet = previous ? asNumber(previous[`${category.key}_net`]) : null
              const dLong = long !== null && prevLong !== null ? long - prevLong : null
              const dShort = short !== null && prevShort !== null ? short - prevShort : null
              const dNet = net !== null && prevNet !== null ? net - prevNet : null
              return (
                <tr key={category.key}>
                  <td>
                    <Term k={category.term}>{category.label}</Term>
                  </td>
                  <td>{num(long, 0)}</td>
                  <td>{num(short, 0)}</td>
                  <td>{num(net, 0)}</td>
                  <td style={{ color: deltaColor(dLong) }}>
                    {arrow(dLong)}
                    {signed(dLong, 0)}
                  </td>
                  <td style={{ color: deltaColor(dShort) }}>
                    {arrow(dShort)}
                    {signed(dShort, 0)}
                  </td>
                  <td style={{ color: deltaColor(dNet) }}>
                    {arrow(dNet)}
                    {signed(dNet, 0)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <div className="chart-note">
          读法：净持仓上升，如果<b>多头同时明显变大</b>，那是真金白银在买涨；
          如果多头没怎么动、<b>空头大幅变小</b>，那多半只是空头认输离场，含义完全不同。
          横线「—」表示算不出，不是 0。
        </div>
        <GlossaryPanel />
      </section>

      <div className="notice">
        范围自 <span className="mono">{startDate || '全部'}</span> 起，共{' '}
        <span className="mono">{rows.length}</span> 期；可见性口径{' '}
        <span className="mono">{selection.pitMode}</span>
        {selection.asOf ? `，读取截至 ${selection.asOf}` : '，读取截至当前时刻'}。
        所有数值均来自原始报告，未做任何平滑。
      </div>
    </>
  )
}