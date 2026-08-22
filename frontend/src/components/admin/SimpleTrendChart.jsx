// 纯 SVG 折线图，不依赖任何图表库——参照的另一个项目（Ragent AI）用的是自己
// 写的 SimpleLineChart，这里保持同样的思路：项目里目前没有引入图表库
// （package.json 里只有 antd/axios/dompurify/marked），运营仪表盘只需要画
// 简单的时间序列趋势，没必要为这一个页面新增一个依赖。
const WIDTH = 560
const HEIGHT = 160
const PAD_LEFT = 44
const PAD_RIGHT = 12
const PAD_TOP = 12
const PAD_BOTTOM = 28

function formatBucketLabel(bucket, window) {
  const d = new Date(bucket)
  if (Number.isNaN(d.getTime())) return bucket
  if (window === '24h') return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

export default function SimpleTrendChart({ points, window, color = '#2563eb', valueFormatter, loading }) {
  if (loading) {
    return <div className="trend-chart-empty">加载中…</div>
  }
  if (!points || points.length === 0) {
    return <div className="trend-chart-empty">暂无数据</div>
  }

  const values = points.map((p) => p.value)
  const maxValue = Math.max(...values, 0)
  const minValue = Math.min(...values, 0)
  const span = maxValue - minValue || 1

  const plotWidth = WIDTH - PAD_LEFT - PAD_RIGHT
  const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM

  const coords = points.map((p, i) => {
    const x = points.length === 1 ? PAD_LEFT : PAD_LEFT + (i / (points.length - 1)) * plotWidth
    const y = PAD_TOP + plotHeight - ((p.value - minValue) / span) * plotHeight
    return { x, y, ...p }
  })

  const linePath = coords.map((c, i) => `${i === 0 ? 'M' : 'L'} ${c.x.toFixed(1)} ${c.y.toFixed(1)}`).join(' ')
  const areaPath = `${linePath} L ${coords[coords.length - 1].x.toFixed(1)} ${(PAD_TOP + plotHeight).toFixed(1)} `
    + `L ${coords[0].x.toFixed(1)} ${(PAD_TOP + plotHeight).toFixed(1)} Z`

  // 横轴只挑首/中/尾三个点打标签，点数少的时候（比如 24h 窗口没几个小时有数据）避免重叠
  const labelIndexes = new Set([0, coords.length - 1, Math.floor((coords.length - 1) / 2)])

  return (
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="trend-chart-svg" preserveAspectRatio="none">
      {[0, 0.5, 1].map((t) => {
        const y = PAD_TOP + plotHeight * t
        const value = maxValue - span * t
        return (
          <g key={t}>
            <line x1={PAD_LEFT} y1={y} x2={WIDTH - PAD_RIGHT} y2={y} className="trend-chart-gridline" />
            <text x={PAD_LEFT - 6} y={y + 3} textAnchor="end" className="trend-chart-axis-label">
              {valueFormatter ? valueFormatter(value) : Math.round(value)}
            </text>
          </g>
        )
      })}
      <path d={areaPath} fill={color} opacity={0.08} stroke="none" />
      <path d={linePath} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      {coords.map((c, i) => (
        <circle key={i} cx={c.x} cy={c.y} r={2.5} fill={color} />
      ))}
      {coords.map((c, i) => (
        labelIndexes.has(i) ? (
          <text key={`label-${i}`} x={c.x} y={HEIGHT - 8} textAnchor="middle" className="trend-chart-axis-label">
            {formatBucketLabel(c.bucket, window)}
          </text>
        ) : null
      ))}
    </svg>
  )
}
