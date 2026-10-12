/**
 * Plain-language glossary.
 *
 * The dashboard is full of terms that mean something precise and nothing at all
 * to a first-time reader ("净持仓", "COT Index", "3 年分位"). Every label that
 * uses one of them is rendered through this table, so the same wording appears
 * in the hover hint and in the "这些词是什么意思" panel below the chart.
 */

export interface GlossaryEntry {
  /** Stable id, also used as the key of `Term`. */
  key: string
  /** The professional term, as it appears on screen. */
  term: string
  /** One sentence a non-specialist can act on. */
  plain: string
  /** How to read the number: which direction means what. */
  reading: string
}

export const GLOSSARY: GlossaryEntry[] = [
  {
    key: 'mm_long',
    term: '多头（看涨的仓位）',
    plain: '基金里押注黄金上涨的合约数量。',
    reading: '数字变大＝这周在看涨方向上加仓。',
  },
  {
    key: 'mm_short',
    term: '空头（看跌的仓位）',
    plain: '基金里押注黄金下跌的合约数量。',
    reading: '数字变大＝这周在看跌方向上加仓。',
  },
  {
    key: 'net',
    term: '净持仓',
    plain: '多头减空头。正数＝基金整体看涨，负数＝基金整体看跌。',
    reading: '看它的大小和方向：越高说明基金越看好黄金。',
  },
  {
    key: 'net_change',
    term: '周度净变化',
    plain: '这一周的净持仓比上一周多了多少。',
    reading: '正数＝这周在加仓看涨；负数＝这周在撤退。',
  },
  {
    key: 'd_long',
    term: '多头周变化',
    plain: '这一周多头仓位增加了多少。',
    reading: '和「空头周变化」分开看，才能分清是「新开多头」还是「空头回补」。',
  },
  {
    key: 'd_short',
    term: '空头周变化',
    plain: '这一周空头仓位增加了多少。',
    reading: '空头减少会让净持仓上升，但那不是看涨加仓，是空头认输离场。',
  },
  {
    key: 'net_streak',
    term: '连续同向周数',
    plain: '已经连续几周朝同一个方向变化（连续加仓或连续减仓）。',
    reading: '周数越长，说明这个动作越是有计划的，不是一周的噪音。',
  },
  {
    key: 'open_interest',
    term: '未平仓量',
    plain: '市场上还没有结算的合约总数，相当于这个市场有多大。',
    reading: '它变大说明有更多资金进场，和价格方向无关。',
  },
  {
    key: 'net_oi_pct',
    term: '净持仓占未平仓量',
    plain: '净持仓占整个市场的比例，用来看基金的仓位相对市场有多重。',
    reading: '比例高＝基金的方向性押注在市场里占比大。它补充绝对手数，但替代不了。',
  },
  {
    key: 'pct_rank_3y',
    term: '3 年历史分位',
    plain: '把现在的净持仓和过去 3 年的每一周比，看它排在什么位置。',
    reading: '90%＝比过去 3 年里 90% 的时间都高（相对高位）；10% 以下＝相对低位。是位置标签，不是买卖信号。',
  },
  {
    key: 'pct_rank_5y',
    term: '5 年历史分位',
    plain: '同上，但比较的是过去 5 年。',
    reading: '窗口更长，结果会比 3 年分位温和一些。',
  },
  {
    key: 'cot_index',
    term: 'COT Index',
    plain: '把净持仓压缩到 0～100 的刻度，方便和别的窗口比。',
    reading: '100＝窗口内最高，0＝窗口内最低。窗口内没有波动时无法计算，会显示为无效。',
  },
  {
    key: 'ret',
    term: '价格收益',
    plain: '黄金价格在过去若干周的涨跌幅。',
    reading: '正数＝这段时间在涨。没有价格数据时保持无效，不会填 0。',
  },
  {
    key: 'trend',
    term: '价格趋势',
    plain: '价格在观察窗口里整体是往上还是往下。',
    reading: '用来和持仓变化对比，判断「价格与仓位是否一致」。',
  },
  {
    key: 'divergence',
    term: '价格与仓位关系',
    plain: '价格方向与基金净持仓变化方向是否一致。',
    reading: '一致＝这波行情有基金配合；背离＝值得再多看一眼，但不代表行情必然反转。',
  },
  {
    key: 'alert',
    term: '极端仓位标记',
    plain: '净持仓处于历史高位（分位 >90%）或历史低位（<10%）时的提示。',
    reading: '拥挤位置只代表「历史上这种位置值得留意」，不是卖出或买入建议。',
  },
  {
    key: 'report_date',
    term: '数据截至（持仓统计日）',
    plain: '报告统计的那一周的周二，也就是持仓真正发生的时间。',
    reading: '这是持仓的时间，不是你看到它的时间。',
  },
  {
    key: 'published_at',
    term: '实际发布日',
    plain: '这份数据你最早能看到的时间（通常是同一周的周五 15:30 美东时间）。',
    reading: '回看历史时必须按这个时间，否则就是在偷看未来。',
  },
  {
    key: 'pit_mode',
    term: '可见性口径',
    plain: '决定「在某一天，当时到底能看见哪些报告」。',
    reading: '默认按公开时间；「按报告日」会看到当时还拿不到的数据，仅适合研究对照。',
  },
  {
    key: 'as_of',
    term: '截至时间（as_of）',
    plain: '把界面还原到某一天的视角，看看那天能看到什么。',
    reading: '留空＝按当前最新数据。',
  },
  {
    key: 'calculation_version',
    term: '计算版本',
    plain: '指标公式的版本号。公式改了才会变。',
    reading: '改阈值不算换公式，所以调阈值不会改变这里。',
  },
  {
    key: 'managed_money',
    term: '基金（管理基金）',
    plain: '对冲基金、商品基金这类投机资金，是追涨杀跌的主力。',
    reading: '它的净持仓最常被拿来代表「市场情绪」，但只能代表这类资金，不能代表整个市场。',
  },
  {
    key: 'producer',
    term: '生产商（商业头寸）',
    plain: '金矿、冶炼厂、加工商这类真正经手黄金的机构，主要用来套期保值。',
    reading: '它们天然偏空（提前锁定卖出价），所以净持仓常为负数，看趋势比看绝对值有用。',
  },
  {
    key: 'swap',
    term: '掉期交易商',
    plain: '银行等做市商，替客户做场外对冲，再把风险转到期货上。',
    reading: '持仓方向常和基金相反，可以理解为「接盘的一方」。',
  },
  {
    key: 'other_reportables',
    term: '其他可报告交易者',
    plain: '达到报告门槛，但不属于上面三类的机构。',
    reading: '数量不大，用来补全「谁在买卖」的拼图，避免只剩基金一个视角。',
  },
  {
    key: 'nonreportable',
    term: '非报告（小户）',
    plain: '没达到报告门槛的小账户，通常被当作散户。',
    reading: '常被当成反向参考：他们越拥挤，往往越接近行情尾声——只是参考，不是信号。',
  },
]

export const TERM_BY_KEY: Record<string, GlossaryEntry> = Object.fromEntries(
  GLOSSARY.map((entry) => [entry.key, entry]),
)

/** Indicator codes returned by the API mapped onto glossary keys. */
export const INDICATOR_TERM_KEY: Record<string, string> = {
  net: 'net',
  d_net: 'net_change',
  d_long: 'd_long',
  d_short: 'd_short',
  net_streak: 'net_streak',
  net_oi_pct: 'net_oi_pct',
  open_interest: 'open_interest',
  pct_rank_3y: 'pct_rank_3y',
  pct_rank_5y: 'pct_rank_5y',
  cot_index_26w: 'cot_index',
  cot_index_52w: 'cot_index',
  cot_index_156w: 'cot_index',
  ret_1w: 'ret',
  ret_4w: 'ret',
  ret_12w: 'ret',
  trend_4w: 'trend',
  trend_12w: 'trend',
  divergence: 'divergence',
  alert: 'alert',
}