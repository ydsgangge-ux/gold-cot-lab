import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import { PricePoint } from '../api'
import { num } from '../format'

interface Props {
  /** The price window around the review date, already sliced by the page. */
  segment: PricePoint[]
  /** Trade date whose close was the "as known then" reference price. */
  anchorTradeDate: string
  /** How many weeks after the anchor the segment covers. */
  horizonWeeks: number
}

const AXIS_LABEL = { color: '#6b7280', fontSize: 11 }
const PRICE_COLOR = '#c98b2f'

/**
 * The price around a review date, so "what happened next" is visible rather
 * than asserted.
 *
 * A vertical marker splits what was knowable at the review date from what came
 * after it. That split is the entire point of the chart, so it is drawn
 * explicitly instead of being described in the caption.
 */
export default function WeeklyPriceChart({ segment, anchorTradeDate, horizonWeeks }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    const el = container.current
    if (!el) return
    chart.current = echarts.init(el, undefined, { renderer: 'canvas' })
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
    const dates = segment.map((point) => point.trade_date)
    const closes = segment.map((point) => (typeof point.close === 'number' ? point.close : null))
    const anchorIndex = dates.indexOf(anchorTradeDate)
    const lastIndex = dates.length - 1

    const option: echarts.EChartsOption = {
      animation: false,
      grid: { left: 70, right: 24, top: 30, bottom: 40 },
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
            return `${point.marker ?? ''}${point.seriesName ?? ''}：${
              value === null ? '—' : num(value, 1)
            }`
          })
          return [head, ...body].join('<br/>')
        },
      },
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLabel: { ...AXIS_LABEL, hideOverlap: true },
        axisLine: { lineStyle: { color: '#dfe2e6' } },
      },
      yAxis: {
        type: 'value',
        name: '美元/盎司',
        nameTextStyle: { color: PRICE_COLOR, fontSize: 11 },
        scale: true,
        axisLabel: { ...AXIS_LABEL, color: PRICE_COLOR, formatter: (value: number) => num(value, 0) },
        splitLine: { lineStyle: { color: '#eceef1' } },
      },
      series: [
        {
          name: '黄金收盘价',
          type: 'line',
          showSymbol: false,
          lineStyle: { width: 1.8, color: PRICE_COLOR },
          itemStyle: { color: PRICE_COLOR },
          data: closes,
          markArea:
            anchorIndex >= 0 && lastIndex > anchorIndex
              ? {
                  silent: true,
                  itemStyle: { color: 'rgba(63,110,168,.06)' },
                  label: {
                    show: true,
                    position: 'insideTop',
                    color: '#9aa1a9',
                    fontSize: 11,
                    formatter: `回看日之后的 ${horizonWeeks} 周（当时还看不到）`,
                  },
                  data: [[{ xAxis: dates[anchorIndex] }, { xAxis: dates[lastIndex] }]],
                }
              : undefined,
          markLine:
            anchorIndex >= 0
              ? {
                  silent: true,
                  symbol: 'none',
                  lineStyle: { color: '#3f6ea8', type: 'dashed', width: 1.2 },
                  label: {
                    formatter: '回看日',
                    color: '#3f6ea8',
                    fontSize: 11,
                    position: 'insideEndTop',
                  },
                  data: [{ xAxis: dates[anchorIndex] }],
                }
              : undefined,
        },
      ],
    }

    chart.current.setOption(option, true)
  }, [segment, anchorTradeDate, horizonWeeks])

  return (
    <>
      <div className="chart" style={{ height: 320 }} ref={container} />
      <div className="chart-note">
        <strong>怎么看这张图：</strong>橙色是 COMEX 黄金期货收盘价，蓝色虚线是<b>回看日</b>，
        线左边是当时已经能看到的行情，右边是回看日<b>之后</b>才发生的走势（阴影部分）。
      </div>
    </>
  )
}