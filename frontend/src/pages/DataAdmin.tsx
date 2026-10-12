import { useCallback, useEffect, useState } from 'react'
import { api, AdminStatus, BatchSummary, DbStats, IngestionBatch } from '../api'
import { num, stampUtc } from '../format'

interface Props {
  reportType: string
  onChanged: () => void
}

/** Batch status codes spelled out; "partial" means "held back for review". */
const STATUS_LABELS: Record<string, string> = {
  ok: '成功',
  running: '进行中',
  partial: '有出入，已保留旧值待复核',
  failed: '失败（持仓数据未受影响）',
  dry_run: '仅校验（未写库）',
}

export default function DataAdmin({ reportType, onChanged }: Props) {
  const [status, setStatus] = useState<AdminStatus | null>(null)
  const [stats, setStats] = useState<DbStats | null>(null)
  const [batches, setBatches] = useState<IngestionBatch[]>([])
  const [detail, setDetail] = useState<IngestionBatch | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [file, setFile] = useState<File | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const [nextStatus, nextStats, nextBatches] = await Promise.all([
        api.adminStatus(),
        api.dbStats(reportType),
        api.ingestions(10),
      ])
      setStatus(nextStatus)
      setStats(nextStats)
      setBatches(nextBatches.batches)
    } catch (reason) {
      setError((reason as Error).message)
    }
  }, [reportType])

  useEffect(() => {
    load()
  }, [load])

  async function runImport(dryRun: boolean) {
    setBusy(true)
    setMessage(null)
    setError(null)
    try {
      const result = await api.importCsv(dryRun)
      const lines = result.batches.map(
        (batch: BatchSummary) =>
          `${batch.source_file}：读入 ${batch.rows_read} 行，新增 ${batch.rows_inserted}，` +
          `更新 ${batch.rows_updated}，未变化 ${batch.rows_unchanged}，待复核 ${batch.rows_revised}（状态 ${batch.status}）`,
      )
      setMessage(`${dryRun ? '校验（未写库）' : '导入'}完成。\n${lines.join('\n')}`)
      await load()
      onChanged()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  /** Send the chosen file's text to the server, which archives it then imports it. */
  async function runUpload(dryRun: boolean) {
    if (!file) {
      setError('请先点上面的「选择文件」，挑一份 CFTC 导出的 CSV。')
      return
    }
    setBusy(true)
    setMessage(null)
    setError(null)
    try {
      const text = await file.text()
      const result = await api.uploadCsv(file.name, text, dryRun)
      const batch = result.batches[0]
      setMessage(
        `${dryRun ? '校验（未写库）' : '导入'}完成：${batch.source_file}\n` +
          `读入 ${batch.rows_read} 行，新增 ${batch.rows_inserted}，更新 ${batch.rows_updated}，` +
          `未变化 ${batch.rows_unchanged}，待复核 ${batch.rows_revised}（状态 ${batch.status}）\n` +
          `原始文件已原样存档到：${result.saved_to}`,
      )
      setFile(null)
      await load()
      onChanged()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  /** Fetch gold prices, then recompute so the price-linked indicators turn valid. */
  async function refreshPrice() {
    setBusy(true)
    setMessage(null)
    setError(null)
    try {
      const result = await api.refreshPrice()
      const price = result.price
      if (price.status === 'ok') {
        const lines = [
          `黄金价格已更新：来源 ${price.source}（代码 ${price.source_symbol}），` +
            `写入 ${num(price.rows_valid ?? 0, 0)} 个交易日。`,
        ]
        if (result.recompute) {
          lines.push(`价格相关指标已重算：${num(result.recompute.rows_written, 0)} 行。`)
        }
        if (price.jump_candidates?.length) {
          lines.push(
            `其中 ${price.jump_candidates.length} 处单日涨跌超过 8%，可能是合约换月，已原样保留、没有做平滑：`,
          )
          lines.push(...price.jump_candidates.map((item) => `· ${item}`))
        }
        setMessage(lines.join('\n'))
      } else {
        const attempts = price.error?.attempts ?? []
        setMessage(
          '这次没能拿到黄金价格（不影响已经存好的持仓数据）。各数据源的结果：\n' +
            (attempts.length
              ? attempts
                  .map((item) => `· ${item.source}：${item.reason}${item.detail ? ` — ${item.detail}` : ''}`)
                  .join('\n')
              : '· 没有可用数据源'),
        )
      }
      await load()
      onChanged()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function openDetail(batchId: string) {
    try {
      setDetail(await api.ingestion(batchId))
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  async function exportCsv() {
    try {
      const data = await api.series(`report_type=${encodeURIComponent(reportType)}&market_code=088691`)
      const columns = [
        'report_date',
        'published_at',
        'mm_long',
        'mm_short',
        'net',
        'd_net',
        'open_interest',
        'net_oi_pct',
        'pct_rank_3y',
        'pct_rank_5y',
        'cot_index_26w',
        'cot_index_52w',
        'cot_index_156w',
      ]
      const lines = [columns.join(',')]
      for (const row of data.series) {
        lines.push(columns.map((column) => (row[column] === null ? '' : String(row[column]))).join(','))
      }
      const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `gold_cot_${reportType}_088691.csv`
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (reason) {
      setError((reason as Error).message)
    }
  }

  const lastBatch = status?.last_batch as Record<string, string | number | null> | null | undefined
  const failure = status?.last_error
  const lastBatchStarted = String(lastBatch?.started_at ?? '')
  // A failure is only worth alarming about if nothing succeeded after it. The
  // log keeps every attempt forever, so an old failure would otherwise read as
  // a current problem on a page whose whole job is to be unambiguous.
  const failureSuperseded = Boolean(
    failure && lastBatchStarted && lastBatchStarted > failure.started_at,
  )

  return (
    <>
      <section className="panel">
        <h2>第一步：导入 CFTC 持仓数据</h2>
        <div className="hint">
          去 <a href="https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm" target="_blank" rel="noreferrer">
            CFTC 官网
          </a>{' '}
          下载最新一期 <b>Disaggregated Futures Only</b> 的 CSV，然后用下面这个按钮直接选中它。
          文件会先原样存档到 <span className="mono">data/raw/cftc/</span>，再解析入库——
          和手工复制进去的效果完全一样。
        </div>

        <div className="upload">
          <input
            type="file"
            accept=".csv,text/csv"
            disabled={busy}
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null)
              setMessage(null)
              setError(null)
            }}
          />
          <span className="file-name">
            {file ? `已选择：${file.name}（${num(file.size / 1024, 0)} KB）` : '还没有选择文件'}
          </span>
        </div>

        <div className="row" style={{ marginTop: 10 }}>
          <button className="action" disabled={busy || !file} onClick={() => runUpload(false)}>
            导入这个文件
          </button>
          <button className="action" disabled={busy || !file} onClick={() => runUpload(true)}>
            先校验，不写入
          </button>
          <button className="action" disabled={busy} onClick={() => runImport(false)}>
            导入 data/raw/cftc 里已有的全部 CSV
          </button>
          <button className="action" disabled={busy} onClick={() => runImport(true)}>
            仅校验上面那些，不写入
          </button>
        </div>

        <h2 style={{ marginTop: 20 }}>第二步：获取黄金价格</h2>
        <div className="hint">
          点一下就会去取 COMEX 黄金期货的日线（依次尝试新浪财经 → Yahoo → stooq → 本地文件），
          拿到后自动重算指标，这样「价格与持仓是否一致」才有结果。
          <b>拿不到也不影响持仓数据</b>，只是价格相关的指标继续显示为算不出。
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="action" disabled={busy} onClick={refreshPrice}>
            获取黄金价格并重算指标
          </button>
          <button className="action" disabled={busy} onClick={exportCsv}>
            导出 088691 周度 CSV
          </button>
          <span className="spacer" />
          <button className="action" disabled={busy} onClick={load}>
            刷新本页
          </button>
        </div>

        {message && <div className="notice ok" style={{ whiteSpace: 'pre-wrap' }}>{message}</div>}
        {error && <div className="notice danger">操作失败：{error}</div>}

        <div className="notice">
          <b>为什么要先存档再解析：</b>
          官方 194 列会被完整保存在 <span className="mono">cot_raw.raw_json</span> 里，每条记录都能回溯到原始文件和行号。
          将来发现指标算错了，直接重算即可，不用重新下载历史。
          <br />
          <b>重复导入不会重复计数：</b>同一份文件导两次，记录数与指标都不变。
        </div>
      </section>

      <section className="panel">
        <h2>最近一次更新</h2>
        {lastBatch ? (
          <dl className="kv">
            <dt>批次号</dt>
            <dd className="mono">{String(lastBatch.batch_id)}</dd>
            <dt>状态</dt>
            <dd style={{ color: String(lastBatch.status) === 'failed' ? '#b4483f' : undefined }}>
              {STATUS_LABELS[String(lastBatch.status)] ?? String(lastBatch.status)}
            </dd>
            <dt>开始 / 结束</dt>
            <dd>
              {stampUtc(String(lastBatch.started_at ?? ''))} → {stampUtc(String(lastBatch.finished_at ?? ''))}
            </dd>
            <dt>耗时</dt>
            <dd>{lastBatch.duration_ms === null ? '—' : `${num(lastBatch.duration_ms as number, 0)} ms`}</dd>
            <dt>来源文件</dt>
            <dd className="mono">{String(lastBatch.source_file ?? '—')}</dd>
            <dt>计数</dt>
            <dd>
              读入 {num(lastBatch.rows_read as number, 0)} · 新增 {num(lastBatch.rows_inserted as number, 0)} ·
              未变化 {num(lastBatch.rows_unchanged as number, 0)} · 待复核 {num(lastBatch.rows_revised as number, 0)}
            </dd>
          </dl>
        ) : (
          <div className="notice">尚无更新记录。</div>
        )}
        {failure && (
          <div className="notice danger">
            <b>最近一次失败</b>：{stampUtc(failure.started_at)}（批次{' '}
            <span className="mono">{failure.batch_id}</span>）
            <div style={{ marginTop: 4 }}>
              {failure.error_json?.message ?? '详情见下方更新日志。'}
            </div>
            {!!failure.error_json?.attempts?.length && (
              <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                {failure.error_json.attempts.map((attempt) => (
                  <li key={attempt.source}>
                    <span className="mono">{attempt.source}</span>：{attempt.reason}
                    {attempt.detail ? ` — ${attempt.detail}` : ''}
                  </li>
                ))}
              </ul>
            )}
            {failureSuperseded && (
              <div style={{ marginTop: 6 }}>
                这条失败发生在上面那次成功更新<b>之前</b>，之后已经成功取到数据，<b>当前数据不受影响</b>。
                它只是台账里留下的一条历史记录，不需要处理。
              </div>
            )}
          </div>
        )}
      </section>

      <section className="panel">
        <h2>数据健康检查</h2>
        <div className="cards">
          <div className="card">
            <div className="label">原始记录数</div>
            <div className="value">{num(status?.counts.cot_raw ?? 0, 0)}</div>
            <div className="meta">官方 194 列原样保留的行数</div>
          </div>
          <div className="card">
            <div className="label">有效报告数</div>
            <div className="value">{num(status?.counts.cot_weekly ?? 0, 0)}</div>
            <div className="meta">清洗后可分析的报告期数</div>
          </div>
          <div className="card">
            <div className="label">指标行数</div>
            <div className="value">{num(status?.counts.cot_indicators ?? 0, 0)}</div>
            <div className="meta">每期报告派生出的各项读数</div>
          </div>
          <div className="card">
            <div className="label">重复的业务主键</div>
            <div className={`value ${(stats?.integrity.duplicate_keys ?? 0) > 0 ? 'down' : ''}`}>
              {num(stats?.integrity.duplicate_keys ?? 0, 0)}
            </div>
            <div className="meta">唯一键 = 口径 + 市场 + 报告日，应为 0</div>
          </div>
          <div className="card">
            <div className="label">无法回溯原始行的记录</div>
            <div className={`value ${(stats?.integrity.weekly_rows_without_raw ?? 0) > 0 ? 'down' : ''}`}>
              {num(stats?.integrity.weekly_rows_without_raw ?? 0, 0)}
            </div>
            <div className="meta">应为 0；不为 0 说明有记录找不到出处</div>
          </div>
          <div className="card">
            <div className="label">黄金价格</div>
            <div className="value" style={{ fontSize: 16 }}>
              {(status?.price_symbols.length ?? 0) === 0
                ? '还没有'
                : `${num(status?.price_symbols[0].rows ?? 0, 0)} 个交易日`}
            </div>
            <div className="meta">
              {(status?.price_symbols.length ?? 0) === 0
                ? '点上面第二步的按钮获取'
                : `${status?.price_symbols[0].symbol} · ${status?.price_symbols[0].first_date} 起`}
            </div>
            <div className="plain">
              没有价格时，「价格与持仓是否一致」这类指标会显示为「暂时算不出」，不会拿 0 来充数。
            </div>
          </div>
        </div>
        <div className="hint" style={{ marginTop: 10 }}>
          这些数字是<b>健康检查</b>用的，正常情况下应该是：原始记录数 = 有效报告数 = 1233，
          重复业务主键 = 0，无法回溯原始行的记录 = 0。只要这几个都对得上，说明数据没有丢、没有重。
        </div>
      </section>

      <section className="panel">
        <h2>市场与日期范围</h2>
        <table className="data">
          <thead>
            <tr>
              <th title="合约名称，括号里是 CFTC 的市场代码">市场</th>
              <th title="已入库的报告期数">报告数</th>
              <th title="最早的一期">首期</th>
              <th title="最新的一期">末期</th>
              <th title="相邻两期超过 7 天就算一个缺口，通常是休市周">缺口数</th>
              <th title="最新一期是什么时候公开的">最近一期公开时间</th>
            </tr>
          </thead>
          <tbody>
            {(stats?.markets ?? []).map((market) => (
              <tr key={market.market_code}>
                <td style={{ textAlign: 'left' }}>
                  {market.market_name ?? '未知合约'}{' '}
                  <span className="mono" style={{ color: '#9aa1a9' }}>{market.market_code}</span>
                </td>
                <td>{num(market.reports, 0)}</td>
                <td>{market.first_date ?? '—'}</td>
                <td>{market.last_date ?? '—'}</td>
                <td>{num(market.gap_count, 0)}</td>
                <td>
                  {stampUtc(
                    status?.markets.find((item) => item.market_code === market.market_code)?.last_published_at,
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="chart-note">
          「缺口」指相邻两期报告间隔超过 7 天。黄金期货的缺口集中在元旦、独立日等休市周，
          属于正常的日历空档，<b>不是数据丢失</b>；下面的日志里可以逐条核对。
        </div>
      </section>

      <section className="panel">
        <h2>更新日志</h2>
        <table className="data">
          <thead>
            <tr>
              <th>批次号</th>
              <th>开始时间</th>
              <th>状态</th>
              <th>来源文件</th>
              <th>读入</th>
              <th>新增</th>
              <th>未变化</th>
              <th>待复核</th>
              <th>校验</th>
            </tr>
          </thead>
          <tbody>
            {batches.map((batch) => (
              <tr key={batch.batch_id}>
                <td className="mono">{batch.batch_id}</td>
                <td>{stampUtc(batch.started_at)}</td>
                <td style={{ color: batch.status === 'failed' ? '#b4483f' : undefined }}>
                  {STATUS_LABELS[batch.status] ?? batch.status}
                </td>
                <td className="mono">{batch.source_file ?? '—'}</td>
                <td>{num(batch.rows_read, 0)}</td>
                <td>{num(batch.rows_inserted, 0)}</td>
                <td>{num(batch.rows_unchanged, 0)}</td>
                <td>{num(batch.rows_revised, 0)}</td>
                <td>
                  <button className="action" onClick={() => openDetail(batch.batch_id)}>
                    查看
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {detail && (
          <div style={{ marginTop: 14 }}>
            <div className="row">
              <strong className="mono">{detail.batch_id}</strong>
              <span className="spacer" />
              <button className="action" onClick={() => setDetail(null)}>
                收起
              </button>
            </div>
            {detail.error_json && (
              <div className="notice danger">
                {detail.error_json.type}: {detail.error_json.message}
              </div>
            )}
            {detail.issues_json && detail.issues_json.length > 0 && (
              <div className="notice warn" style={{ whiteSpace: 'pre-wrap' }}>
                {detail.issues_json.join('\n')}
              </div>
            )}
            <table className="data" style={{ marginTop: 8 }}>
              <thead>
                <tr>
                  <th>规则</th>
                  <th>级别</th>
                  <th>结果</th>
                  <th>检查条数</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {(detail.validation_json ?? []).map((rule) => (
                  <tr key={rule.rule}>
                    <td className="mono">{rule.rule}</td>
                    <td>{rule.level}</td>
                    <td style={{ color: rule.passed ? '#3f8a63' : '#b4483f' }}>
                      {rule.passed ? '通过' : `未通过（${rule.samples.length} 例）`}
                    </td>
                    <td>{num(rule.checked, 0)}</td>
                    <td style={{ textAlign: 'left', whiteSpace: 'normal', color: '#6b7280' }}>
                      {rule.note ?? ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}