import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import { SeriesRow } from '../api'
import { num } from '../format'

interface Props {
  rows: SeriesRow[]
  /** Gold close per report, already aligned to the weekly report axis. */
  priceSeries: Array<number | null>
  /** Whether any price exists; when none does, the price axis stays hidden. */
  hasPrice: boolean
}

const AXIS_LABEL = { color: '#6b7280', fontSize: 11 }
const SPLIT_LINE = { lineStyle: { color: '#eceef1' } }
const PRICE_COLOR = '#c98b2f'

/**
 * Two stacked charts sharing one x axis.
 *
 * Top: net managed-money position and open interest on the left, gold price on
 * the right — the price line is the whole point, because "is positioning telling
 * the same story as price" only reads correctly on a single timeline.
 * Bottom: trailing-window position (3y percentile) and COT index, both 0-100.
 */
export default function PositionChart({ rows, priceSeries, hasPrice }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    const el = container.current
    if (!el) return
    chart.current = echarts.init(el, undefined, { renderer: 'canvas' })
    // Observing the element, not just the window: on the dev server the stylesheet
    // is injected by script, so the container can still measure 0x0 when the first
    // effect runs and the chart would be drawn into an empty box.
    const observer = new ResizeObserver(() => chart.current?.resize())
    observer.observe(el)
    return () => {
      observer.disconnect()
      chart.current?.dispose()
      chart.current = null
    }
  }, [])

  useEffect(() => {
    if (!chart.current) return
    const dates = rows.map((row) => row.report_date)
    const pick = (key: string): Array<number | null> =>
      rows.map((row) => {
        const value = row[key]
        return typeof value === 'number' ? value : null
      })

    const legendData = ['净持仓', '未平仓量']
    if (hasPrice) legendData.push('黄金价格（右轴）')
    legendData.push('3 年分位', 'COT Index（52 周）')

    const option: echarts.EChartsOption = {
      animation: false,
      grid: [
        { left: 74, right: 84, top: 34, height: '52%' },
        { left: 74, right: 84, top: '72%', height: '19%' },
      ],
      legend: {
        top: 2,
        left: 66,
        itemGap: 16,
        textStyle: { color: '#6b7280', fontSize: 11.5 },
        data: legendData,
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', label: { backgroundColor: '#8a929c' } },
        backgroundColor: 'rgba(255,255,255,.97)',
        borderColor: '#dfe2e6',
        textStyle: { color: '#2b2f33', fontSize: 12 },
        formatter: (params) => {
          const list = Array.isArray(params) ? params : [params]
          const head = list.length
            ? String((list[0] as { axisValue?: string }).axisValue ?? '')
            : ''
          const body = list.map((item) => {
            const point = item as { marker?: string; seriesName?: string; value?: unknown }
            const value = typeof point.value === 'number' ? point.value : null
            // Price needs a decimal; lots and 0-100 indices do not.
            const digits = point.seriesName?.startsWith('黄金价格') ? 1 : 0
            return `${point.marker ?? ''}${point.seriesName ?? ''}：${
              value === null ? '—' : num(value, digits)
            }`
          })
          return [head, ...body].join('<br/>')
        },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      xAxis: [
        {
          type: 'category',
          data: dates,
          boundaryGap: false,
          axisLabel: { ...AXIS_LABEL, hideOverlap: true },
          axisLine: { lineStyle: { color: '#dfe2e6' } },
        },
        {
          type: 'category',
          gridIndex: 1,
          data: dates,
          boundaryGap: false,
          axisLabel: { ...AXIS_LABEL, hideOverlap: true, formatter: (value: string) => value.slice(2, 7) },
          axisLine: { lineStyle: { color: '#dfe2e6' } },
        },
      ],
      yAxis: [
        {
          type: 'value',
          name: '手数',
          nameTextStyle: { color: '#9aa1a9', fontSize: 11 },
          axisLabel: { ...AXIS_LABEL, formatter: (value: number) => num(value / 1000, 0) + 'k' },
          splitLine: SPLIT_LINE,
        },
        {
          type: 'value',
          name: '美元/盎司',
          nameTextStyle: { color: PRICE_COLOR, fontSize: 11 },
          position: 'right',
          show: hasPrice,
          scale: true,
          axisLabel: { ...AXIS_LABEL, color: PRICE_COLOR, formatter: (value: number) => num(value, 0) },
          splitLine: { show: false },
        },
        {
          type: 'value',
          gridIndex: 1,
          min: 0,
          max: 100,
          name: '0-100',
          nameTextStyle: { color: '#9aa1a9', fontSize: 11 },
          axisLabel: AXIS_LABEL,
          splitLine: SPLIT_LINE,
        },
      ],
      series: [
        {
          name: '净持仓',
          type: 'line',
          showSymbol: false,
          lineStyle: { width: 1.8, color: '#3f6ea8' },
          itemStyle: { color: '#3f6ea8' },
          areaStyle: { color: 'rgba(63,110,168,.07)' },
          data: pick('net'),
        },
        {
          name: '未平仓量',
          type: 'line',
          showSymbol: false,
          lineStyle: { width: 1.2, color: '#c0c6cd' },
          itemStyle: { color: '#c0c6cd' },
          data: pick('open_interest'),
        },
        ...(hasPrice
          ? [
              {
                name: '黄金价格（右轴）',
                type: 'line' as const,
                yAxisIndex: 1,
                showSymbol: false,
                lineStyle: { width: 1.6, color: PRICE_COLOR },
                itemStyle: { color: PRICE_COLOR },
                data: priceSeries,
              },
            ]
          : []),
        {
          name: '3 年分位',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 2,
          showSymbol: false,
          lineStyle: { width: 1.6, color: '#b9822f' },
          itemStyle: { color: '#b9822f' },
          data: pick('pct_rank_3y'),
          markLine: {
            silent: true,
            symbol: 'none',
            label: { formatter: '{c}', color: '#9aa1a9', fontSize: 10 },
            lineStyle: { color: '#e3cfa6', type: 'dashed' },
            data: [{ yAxis: 90 }, { yAxis: 10 }],
          },
        },
        {
          name: 'COT Index（52 周）',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 2,
          showSymbol: false,
          lineStyle: { width: 1.6, color: '#3f8a63' },
          itemStyle: { color: '#3f8a63' },
          data: pick('cot_index_52w'),
        },
      ],
    }

    chart.current.setOption(option, true)
  }, [rows, priceSeries, hasPrice])

  return (
    <>
      <div className="chart" ref={container} />
      <div className="chart-note">
        <strong>怎么看这张图：</strong>上面那张——蓝线是基金净持仓（左轴，手数），灰线是市场总规模，
        {hasPrice ? '橙线是 COMEX 黄金期货价格（右轴，美元/盎司）。' : '（还没有价格数据，所以没有价格线。）'}
        <br />
        下面那张——橙线是净持仓的 3 年历史分位，绿线是 52 周 COT Index，两条都按 0～100 的刻度读；
        虚线是 90% / 10% 两条参考线，越靠上说明仓位越拥挤。
        <br />
        <span style={{ color: '#9aa1a9' }}>
          橙线和蓝线如果方向不一致（价格在涨、基金却在减仓），就是常说的「背离」，
          值得多看一眼，但不代表行情一定会反转。所有分位与 COT Index 只使用当期及之前的报告，不含未来数据。
        </span>
      </div>
    </>
  )
}