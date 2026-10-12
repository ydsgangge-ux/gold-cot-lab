import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import { StructureRow } from '../api'
import { num } from '../format'

/** Metric being plotted: net position, gross long, or gross short. */
export type StructureMetric = 'net' | 'long' | 'short'

export const METRIC_LABELS: Record<StructureMetric, string> = {
  net: '净持仓',
  long: '多头',
  short: '空头',
}

/** The five trader categories, in the order the CFTC report lists them. */
export const CATEGORIES: Array<{ key: string; label: string; term: string; color: string }> = [
  { key: 'managed_money', label: '基金（管理基金）', term: 'managed_money', color: '#3f6ea8' },
  { key: 'producer', label: '生产商', term: 'producer', color: '#b9822f' },
  { key: 'swap', label: '掉期交易商', term: 'swap', color: '#3f8a63' },
  { key: 'other_reportables', label: '其他可报告', term: 'other_reportables', color: '#8a67ab' },
  { key: 'nonreportable', label: '非报告（小户）', term: 'nonreportable', color: '#9aa1a9' },
]

interface Props {
  rows: StructureRow[]
  metric: StructureMetric
}

const AXIS_LABEL = { color: '#6b7280', fontSize: 11 }
const SPLIT_LINE = { lineStyle: { color: '#eceef1' } }

/**
 * Every trader category on one timeline.
 *
 * The point of a single axis is comparison: who is long when the fund is long,
 * and who is taking the other side. Adding a second y-axis here would create
 * false crossings, so all five share the same 手数 scale.
 */
export default function StructureChart({ rows, metric }: Props) {
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
    const dates = rows.map((row) => row.report_date)

    const option: echarts.EChartsOption = {
      animation: false,
      grid: { left: 78, right: 26, top: 40, bottom: 46 },
      legend: {
        top: 6,
        itemGap: 16,
        textStyle: { color: '#6b7280', fontSize: 11.5 },
        data: CATEGORIES.map((category) => category.label),
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
            return `${point.marker ?? ''}${point.seriesName ?? ''}：${
              value === null ? '—' : num(value, 0)
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
        name: '手数',
        nameTextStyle: { color: '#9aa1a9', fontSize: 11 },
        axisLabel: { ...AXIS_LABEL, formatter: (value: number) => num(value / 1000, 0) + 'k' },
        splitLine: SPLIT_LINE,
      },
      series: CATEGORIES.map((category) => ({
        name: category.label,
        type: 'line' as const,
        showSymbol: false,
        lineStyle: { width: 1.7, color: category.color },
        itemStyle: { color: category.color },
        data: rows.map((row) => {
          const value = row[`${category.key}_${metric}`]
          return typeof value === 'number' ? value : null
        }),
        markLine:
          metric === 'net' && category.key === 'managed_money'
            ? {
                silent: true,
                symbol: 'none',
                label: { show: false },
                lineStyle: { color: '#c9ced5', type: 'dashed', width: 1 },
                data: [{ yAxis: 0 }],
              }
            : undefined,
      })),
    }

    chart.current.setOption(option, true)
  }, [rows, metric])

  return (
    <>
      <div className="chart" ref={container} />
      <div className="chart-note">
        <strong>怎么看这张图：</strong>同一条时间轴上，每一类交易者各一条线，
        当前显示的是<b>{METRIC_LABELS[metric]}</b>（单位：手）。
        五条线共用同一刻度，方便直接比大小、比方向。
        <br />
        <span style={{ color: '#9aa1a9' }}>
          生产商与掉期交易商天然偏空（多是套保、做市），基金偏多，所以不要拿它们的绝对值互相比较；
          真正要看的是<b>同一类人自己前后的变化</b>，以及谁在给谁接盘。
        </span>
      </div>
    </>
  )
}