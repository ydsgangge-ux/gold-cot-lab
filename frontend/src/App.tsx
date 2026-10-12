import { useEffect, useMemo, useState } from 'react'
import { api, Filters, PitMode } from './api'
import FilterBar, { Selection } from './components/FilterBar'
import Overview from './pages/Overview'
import Structure from './pages/Structure'
import Weekly from './pages/Weekly'
import DataAdmin from './pages/DataAdmin'
import { RangeKey, shiftYears } from './format'

export type TabKey = 'overview' | 'structure' | 'weekly' | 'admin'

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'overview', label: '持仓总览' },
  { key: 'structure', label: '交易者结构' },
  { key: 'weekly', label: '历史周报' },
  { key: 'admin', label: '数据管理' },
]

function readTab(): TabKey {
  const hash = window.location.hash.replace('#', '') as TabKey
  return TABS.some((tab) => tab.key === hash) ? hash : 'overview'
}

export default function App() {
  const [filters, setFilters] = useState<Filters | null>(null)
  const [fatal, setFatal] = useState<string | null>(null)
  const [tab, setTab] = useState<TabKey>(readTab)
  const [reportType, setReportType] = useState('disaggregated_futures_only')
  const [marketCode, setMarketCode] = useState('088691')
  const [range, setRange] = useState<RangeKey>('all')
  const [asOf, setAsOf] = useState('')
  const [pitMode, setPitMode] = useState<PitMode>('published_at')
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    api
      .filters()
      .then((data) => {
        setFilters(data)
        const preferred = data.markets.find(
          (market) => market.report_type === 'disaggregated_futures_only' && market.market_code === '088691',
        )
        if (preferred) {
          setMarketCode(preferred.market_code)
        } else if (data.markets[0]) {
          setMarketCode(data.markets[0].market_code)
          setReportType(data.markets[0].report_type)
        }
      })
      .catch((error: Error) => setFatal(error.message))
  }, [reloadToken])

  useEffect(() => {
    window.location.hash = tab
  }, [tab])

  useEffect(() => {
    const onHashChange = () => setTab(readTab())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const markets = useMemo(
    () => (filters?.markets ?? []).filter((market) => market.report_type === reportType),
    [filters, reportType],
  )

  useEffect(() => {
    if (markets.length && !markets.some((market) => market.market_code === marketCode)) {
      setMarketCode(markets[0].market_code)
    }
  }, [markets, marketCode])

  const activeMarket = markets.find((market) => market.market_code === marketCode)
  const selection: Selection = { reportType, marketCode, range, asOf, pitMode }

  const startDate = useMemo(() => {
    const anchor = activeMarket?.last_date ?? filters?.latest_published_at?.slice(0, 10) ?? ''
    return shiftYears(anchor, range)
  }, [activeMarket, filters, range])

  function handleReportType(next: string) {
    setReportType(next)
    const first = (filters?.markets ?? []).find((market) => market.report_type === next)
    if (first) setMarketCode(first.market_code)
  }

  if (fatal) {
    return (
      <div className="app">
        <h1>Gold COT Lab</h1>
        <div className="notice danger">
          无法连接本地服务：{fatal}
          <div style={{ marginTop: 6 }}>
            请先在项目根目录运行{' '}
            <span className="mono">python -m uvicorn backend.main:app --port 9527</span>，
            或者直接双击项目里的 <b>启动.bat</b>。
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Gold COT Lab · 黄金持仓分析</h1>
        <span className="tag">本地运行 · 不预测金价</span>
      </header>

      <div className="notice" style={{ marginTop: 10 }}>
        <b>这个工具在回答四个问题：</b>
        ① 基金现在积累了多少净多头，是在加仓还是撤退？
        ② 当前持仓在过去十几年里算高还是算低？
        ③ 黄金涨跌有没有基金持仓配合，还是出现了背离？
        ④ 目前的数据支持哪些判断，哪些其实还缺证据？
        <div style={{ marginTop: 6, color: '#9aa1a9' }}>
          界面里凡是带<span className="term">虚线下划线</span>的词，鼠标停上去就有一句大白话解释；
          「持仓总览」页最下方还有一张完整的术语对照表。
          当前计算版本 <span className="mono">{filters?.effective_params.calculation_version ?? '—'}</span>。
        </div>
      </div>

      <nav className="tabs">
        {TABS.map((item) => (
          <button
            key={item.key}
            data-active={tab === item.key}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <FilterBar
        filters={filters}
        markets={markets}
        selection={selection}
        startDate={startDate}
        onReportType={handleReportType}
        onMarket={setMarketCode}
        onRange={setRange}
        onAsOf={setAsOf}
        onPitMode={setPitMode}
      />

      {tab === 'overview' && (
        <Overview selection={selection} startDate={startDate} activeMarket={activeMarket} />
      )}

      {tab === 'admin' && (
        <DataAdmin
          reportType={reportType}
          onChanged={() => setReloadToken((token) => token + 1)}
        />
      )}

      {tab === 'structure' && <Structure selection={selection} startDate={startDate} />}

      {tab === 'weekly' && (
        <Weekly selection={selection} startDate={startDate} onAsOf={setAsOf} />
      )}

      <div className="notice">
        本工具不预测金价。所有结论都可回溯到净持仓变化、历史分位与价格走势；
        分位与 COT Index 使用向后滚动窗口，不会把未来数据混入历史值。
      </div>
    </div>
  )
}