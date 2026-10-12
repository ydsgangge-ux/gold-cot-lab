import { Filters, MarketOption, PitMode, REPORT_TYPE_LABELS } from '../api'
import { RANGE_LABELS, RangeKey, stampUtc } from '../format'
import { Term } from './Term'

export interface Selection {
  reportType: string
  marketCode: string
  range: RangeKey
  asOf: string
  pitMode: PitMode
}

interface Props {
  filters: Filters | null
  markets: MarketOption[]
  selection: Selection
  startDate: string
  onReportType: (value: string) => void
  onMarket: (value: string) => void
  onRange: (value: RangeKey) => void
  onAsOf: (value: string) => void
  onPitMode: (value: PitMode) => void
}

export default function FilterBar(props: Props) {
  const { filters, markets, selection, startDate } = props
  const { reportType, marketCode, range, asOf, pitMode } = selection
  const activeMarket = markets.find((market) => market.market_code === marketCode)

  return (
    <section className="panel">
      <h2>筛选（一般不用动，默认就是最常用的设置）</h2>
      <div className="filters">
        <div className="field">
          <label htmlFor="report-type">报告类型</label>
          <select
            id="report-type"
            value={reportType}
            onChange={(event) => props.onReportType(event.target.value)}
          >
            {(filters?.report_types ?? [{ code: reportType, available: true }]).map((item) => (
              <option key={item.code} value={item.code} disabled={!item.available && item.code !== reportType}>
                {REPORT_TYPE_LABELS[item.code] ?? item.code}
                {item.available ? '' : '（尚未导入）'}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="market">看哪个黄金合约</label>
          <select id="market" value={marketCode} onChange={(event) => props.onMarket(event.target.value)}>
            {markets.map((market) => (
              <option key={market.market_code} value={market.market_code}>
                {market.market_name ?? market.market_code} · {market.market_code}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="range">看多长时间</label>
          <select id="range" value={range} onChange={(event) => props.onRange(event.target.value as RangeKey)}>
            {RANGE_LABELS.map((item) => (
              <option key={item.key} value={item.key}>
                {item.label}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="as-of">把视角拉回某一天</label>
          <input
            id="as-of"
            type="date"
            value={asOf.slice(0, 10)}
            onChange={(event) => props.onAsOf(event.target.value)}
          />
        </div>

        <div className="field">
          <label htmlFor="pit-mode">
            <Term k="pit_mode">可见性口径</Term>
          </label>
          <select
            id="pit-mode"
            value={pitMode}
            onChange={(event) => props.onPitMode(event.target.value as PitMode)}
          >
            {(filters?.pit_modes ?? []).map((item) => (
              <option key={item.code} value={item.code}>
                {item.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="notice">
        当前设置下能看到
        <span className="mono">
          {' '}
          {startDate || (activeMarket?.first_date ?? '起始')} → {activeMarket?.last_date ?? '最新'}
        </span>
        的报告，最新一期于 <span className="mono">{stampUtc(activeMarket?.last_published_at)}</span> 公开
        {asOf ? `，读取截至 ${asOf}` : '，读取截至当前时刻'}。
      </div>

      {pitMode === 'report_date' && (
        <div className="notice danger">
          你现在用的是「按报告日」。CFTC 通常要等到那周周五才公开报告，
          所以这个口径会看到<b>当时还拿不到的数据</b>（前视偏差），只能用来做研究对照，不能拿来下结论。
        </div>
      )}
      {pitMode === 'conservative' && (
        <div className="notice warn">
          「保守口径」把可见时间往后推 7 天，用来检验「即便更晚才看到，结论是否还站得住」。
        </div>
      )}
    </section>
  )
}